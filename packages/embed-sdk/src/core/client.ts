import { io, type Socket } from 'socket.io-client';
import type {
  AgentInfo,
  AgentResponseData,
  ClientInitializedData,
  ConversationSnapshotData,
  SendMessagePayload,
  Settings,
} from './wire';
import type { NannosConfig, NannosErrorEvent } from './types';

/**
 * Connection state, mirroring console-frontend's SocketContext semantics:
 * - `socketConnected` — the socket.io transport is up (console: `isSocketReady`)
 * - `initialized`     — `initialize_client` handshake succeeded (console: `isConnected`)
 */
export interface TransportState {
  socketConnected: boolean;
  initialized: boolean;
  agentInfo: AgentInfo | null;
}

const INIT_TIMEOUT_MS = 15_000;

/** Injectable for tests; matches the socket.io `io()` call shape we use. */
export type IoFactory = (uri: string | undefined, opts: Record<string, unknown>) => Socket;

/**
 * Transport client. Wraps the SAME socket.io protocol console-frontend uses to
 * talk to console-backend (`/api/v1/socket.io`; events `initialize_client` /
 * `client_initialized` / `send_message` / `agent_response` / `cancel_task`).
 *
 * Two auth modes:
 * - same-origin (console): no `backendUrl`, cookies carry the session.
 * - embedded (on-behalf-of, ADR-0002): absolute `backendUrl` + `getToken()`;
 *   the token rides the socket.io `auth` payload.
 *
 * Console-only channels (`scheduler_notification`, `call_completed`,
 * `catalog_*_progress`) are deliberately NOT part of this client — they are
 * admin-app concerns and stay in console-frontend's app shell.
 */
export class TransportClient {
  private socket: Socket | null = null;
  private state: TransportState = { socketConnected: false, initialized: false, agentInfo: null };
  private readonly stateListeners = new Set<(s: TransportState) => void>();
  private readonly responseListeners = new Set<(data: AgentResponseData) => void>();
  private readonly errorListeners = new Set<(e: NannosErrorEvent) => void>();
  private pendingInit: ((ok: boolean) => void) | null = null;
  private reauthTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly cfg: NannosConfig,
    private readonly ioFactory: IoFactory = io as unknown as IoFactory,
  ) {}

  /** Decode a JWT's `exp` (epoch ms) without verifying — for scheduling re-auth. */
  private static jwtExpMs(token: string): number | null {
    try {
      const [, payload] = token.split('.');
      let b64 = payload.replace(/-/g, '+').replace(/_/g, '/');
      b64 += '='.repeat((4 - (b64.length % 4)) % 4); // JWT payloads are unpadded base64url
      const json = JSON.parse(atob(b64));
      return typeof json.exp === 'number' ? json.exp * 1000 : null;
    } catch {
      return null;
    }
  }

  /** Reconnect shortly before the access token expires so the connection always
   *  carries a fresh token (socket.io re-runs the auth callback on reconnect →
   *  getToken refreshes/re-mints). Short-lived embed tokens would otherwise go
   *  stale mid-session and break the next orchestrator call. */
  private scheduleReauth(token: string) {
    if (this.reauthTimer) clearTimeout(this.reauthTimer);
    this.reauthTimer = null;
    const exp = TransportClient.jwtExpMs(token);
    if (!exp) return;
    const delay = exp - Date.now() - 60_000; // 60s lead
    if (delay <= 0) return; // already near expiry; the next (re)connect refreshes
    this.reauthTimer = setTimeout(() => {
      if (this.socket) {
        this.socket.disconnect();
        this.socket.connect(); // auth callback re-runs → fresh token → new session
      }
    }, delay);
  }

  getState(): TransportState {
    return this.state;
  }

  /** Subscribe to connection-state changes. Returns an unsubscribe function. */
  subscribe(listener: (s: TransportState) => void): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  private setState(patch: Partial<TransportState>) {
    this.state = { ...this.state, ...patch };
    for (const l of this.stateListeners) l(this.state);
  }

  /** Subscribe to SDK-internal errors (connection / init / auth-token). Returns an
   *  unsubscribe function. */
  onError(listener: (e: NannosErrorEvent) => void): () => void {
    this.errorListeners.add(listener);
    return () => this.errorListeners.delete(listener);
  }

  private emitError(e: NannosErrorEvent) {
    for (const l of this.errorListeners) l(e);
  }

  /** Create the socket and wire protocol events. Resolves once the socket object exists
   *  (connection itself is async — observe it via subscribe()). */
  async connect(): Promise<void> {
    if (this.socket) return;
    const getToken = this.cfg.getToken;
    const opts: Record<string, unknown> = {
      path: this.cfg.socketPath ?? '/api/v1/socket.io',
      // Auth as a CALLBACK, not a static value: socket.io invokes it on the
      // initial connect AND every reconnect, so we always present a FRESH token.
      // The embed's access token is short-lived (~5 min); a captured-once token
      // would go stale and every reconnect would be rejected → "Disconnected".
      // `getToken` (host-provided) refreshes/re-mints as needed.
      ...(getToken
        ? {
            auth: (cb: (data: Record<string, unknown>) => void) => {
              Promise.resolve(getToken())
                .then((t) => {
                  cb(t ? { token: t } : {});
                  if (t) this.scheduleReauth(t);
                })
                .catch((err) => {
                  this.emitError({ type: 'auth', message: 'getToken() failed', cause: err });
                  cb({});
                });
            },
          }
        : {}),
    };
    // Same-origin (console) when no backendUrl; absolute origin when embedded.
    const socket = this.ioFactory(this.cfg.backendUrl, opts);

    socket.on('connect', () => this.setState({ socketConnected: true }));
    socket.on('connect_error', (err: Error) =>
      this.emitError({ type: 'connection', message: err?.message || 'socket connect_error', cause: err }),
    );
    socket.on('disconnect', () =>
      this.setState({ socketConnected: false, initialized: false, agentInfo: null }),
    );
    socket.on('client_initialized', (data: ClientInitializedData) => {
      const ok = data.status === 'success';
      this.setState({ initialized: ok, agentInfo: ok ? (data.agent ?? null) : null });
      if (!ok) {
        this.emitError({
          type: 'init',
          message: 'initialize_client rejected',
          detail: { status: data.status, error: (data as { error?: unknown }).error },
        });
      }
      this.pendingInit?.(ok);
      this.pendingInit = null;
    });
    socket.on('agent_response', (data: AgentResponseData) => {
      for (const l of this.responseListeners) l(data);
    });
    socket.on('conversation_snapshot', (data: ConversationSnapshotData) => {
      // Replay a pending interactive prompt through the normal agent_response path
      // so the existing HITL rendering handles it — a prompt that arrived while
      // disconnected would otherwise never render and the turn would hang waiting
      // for an approval the user can't give.
      if (data?.pendingHitl) {
        for (const l of this.responseListeners) l(data.pendingHitl);
      }
      for (const l of this.snapshotListeners) l(data);
    });
    for (const event of this.extraListeners.keys()) this.attachExtraHandler(socket, event);

    this.socket = socket;
  }

  /**
   * `initialize_client` handshake (mirrors console: 15s timeout, resolves false
   * on error/timeout, stores agentInfo on success).
   */
  initializeClient(settings: Settings, sessionId: string): Promise<boolean> {
    return new Promise((resolve) => {
      if (!this.socket) {
        resolve(false);
        return;
      }
      const timeoutMs = this.cfg.initTimeoutMs ?? INIT_TIMEOUT_MS;
      const timeout = setTimeout(() => {
        this.setState({ initialized: false });
        this.pendingInit = null;
        this.emitError({ type: 'init', message: 'initialize_client timed out', detail: { timeoutMs } });
        resolve(false);
      }, timeoutMs);

      this.pendingInit = (ok: boolean) => {
        clearTimeout(timeout);
        resolve(ok);
      };

      this.socket.emit('initialize_client', {
        url: settings.agentUrl,
        customHeaders: this.cfg.customHeaders ?? {},
        sessionId,
      });
    });
  }

  /** Emit a chat message. No-op (returns false) when the socket is not connected. */
  sendMessage(payload: SendMessagePayload): boolean {
    if (!this.socket?.connected) return false;
    this.socket.emit('send_message', payload);
    return true;
  }

  /** Cancel the running task of a conversation. */
  cancelTask(conversationId: string): boolean {
    if (!this.socket?.connected) return false;
    this.socket.emit('cancel_task', { conversationId });
    return true;
  }

  /** Join a conversation's room (multi-replica); server replies with a `conversation_snapshot`. */
  subscribeConversation(conversationId: string): boolean {
    if (!this.socket?.connected) return false;
    this.socket.emit('subscribe_conversation', { conversationId });
    return true;
  }

  unsubscribeConversation(conversationId: string): boolean {
    if (!this.socket?.connected) return false;
    this.socket.emit('unsubscribe_conversation', { conversationId });
    return true;
  }

  /** Subscribe to conversation resume snapshots (multi-replica protocol). */
  onConversationSnapshot(cb: (data: ConversationSnapshotData) => void): () => void {
    this.snapshotListeners.add(cb);
    return () => this.snapshotListeners.delete(cb);
  }

  private readonly snapshotListeners = new Set<(data: ConversationSnapshotData) => void>();

  /** Subscribe to agent responses (carries all A2A-extension directives). */
  onAgentResponse(cb: (data: AgentResponseData) => void): () => void {
    this.responseListeners.add(cb);
    return () => this.responseListeners.delete(cb);
  }

  /**
   * Generic subscription to any server-sent socket event. Escape hatch for
   * host-app channels that are not part of the chat protocol (e.g. console's
   * `catalog_sync_progress`). Buffers registrations made before connect().
   */
  onEvent(event: string, cb: (data: unknown) => void): () => void {
    let list = this.extraListeners.get(event);
    if (!list) {
      list = new Set();
      this.extraListeners.set(event, list);
      // Attach exactly once per (socket, event): when the map ENTRY is created —
      // not on every 0→1 size transition, which would stack a duplicate socket
      // handler each time a consumer unsubscribes and a later one resubscribes
      // (the handler fans out to the live set, so N handlers fire callbacks N
      // times). Unsubscribing keeps the entry; connect() covers pre-connect
      // registrations and fresh sockets.
      if (this.socket) this.attachExtraHandler(this.socket, event);
    }
    list.add(cb);
    return () => {
      this.extraListeners.get(event)?.delete(cb);
    };
  }

  private readonly extraListeners = new Map<string, Set<(data: unknown) => void>>();

  private attachExtraHandler(socket: Socket, event: string) {
    socket.on(event, (data: unknown) => {
      for (const l of this.extraListeners.get(event) ?? []) l(data);
    });
  }

  /** Force the socket to re-run its auth callback (disconnect → connect) so a
   *  token that was just minted (post-login) is presented. No-op if not connected;
   *  the caller should connect() in that case. */
  reauth(): void {
    if (!this.socket) return;
    this.socket.disconnect();
    this.socket.connect(); // auth callback re-runs → fresh token → new session
  }

  disconnect(): void {
    if (this.reauthTimer) clearTimeout(this.reauthTimer);
    this.reauthTimer = null;
    this.socket?.disconnect();
    this.socket = null;
    this.pendingInit = null;
    this.setState({ socketConnected: false, initialized: false, agentInfo: null });
  }
}
