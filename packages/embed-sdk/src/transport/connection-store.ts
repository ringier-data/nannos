/**
 * Owns the `initialize_client` handshake on top of a connected socket, and
 * answers the transport's `whenReady()`. Re-runs the handshake automatically
 * when the socket reconnects (a reconnect starts a NEW server session that
 * knows nothing until initialized). Framework-free; the panel reads it via
 * subscribe/getSnapshot.
 */
import type { TransportClient } from '../core/client';
import type { Settings } from '../core/wire';

export interface ConnectionSnapshot {
  socketConnected: boolean;
  initialized: boolean;
  agentName: string | null;
}

export class ConnectionStore {
  private snapshot: ConnectionSnapshot = { socketConnected: false, initialized: false, agentName: null };
  private readonly listeners = new Set<() => void>();
  private readonly readyWaiters = new Set<(ok: boolean) => void>();
  private initializing = false;
  private detachClient: (() => void) | null = null;

  constructor(
    private readonly client: TransportClient,
    /** Resolved lazily at init time (agent-URL discovery is async). */
    private readonly getSettings: () => Promise<Settings>,
    private readonly sessionId: string,
    private readonly initTimeoutMs = 10_000,
  ) {
    this.attach();
  }

  /**
   * (Re)subscribe to the client. Idempotent, and callable AFTER `destroy()`:
   * React runs mount → cleanup → mount over the SAME memoized engine
   * (StrictMode, Fast Refresh), and a detached store stops seeing the socket
   * entirely — no re-handshake, and a frozen snapshot behind the panel header.
   */
  attach(): void {
    if (this.detachClient) return;
    this.detachClient = this.client.subscribe((state) => {
      const next: ConnectionSnapshot = {
        socketConnected: state.socketConnected,
        initialized: state.initialized,
        agentName: state.agentInfo?.displayName ?? state.agentInfo?.name ?? this.snapshot.agentName,
      };
      const changed =
        next.socketConnected !== this.snapshot.socketConnected ||
        next.initialized !== this.snapshot.initialized ||
        next.agentName !== this.snapshot.agentName;
      this.snapshot = next;
      if (changed) for (const l of this.listeners) l();
      // State-driven (not promise-driven): a reconnect can race the
      // initialize() continuation, and re-init must not depend on it.
      if (next.initialized) this.settleWaiters(true);
      // Any connected-but-uninitialized socket needs the handshake: a fresh
      // server session after a reconnect, and equally the FIRST connect — the
      // scope's initialize() runs in a child effect, so it can fire before the
      // provider's connect() has even built the socket.
      if (next.socketConnected && !next.initialized && !this.initializing) {
        void this.initialize();
      }
    });
  }

  getSnapshot = (): ConnectionSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Run the handshake (idempotent; concurrent calls coalesce). */
  async initialize(): Promise<boolean> {
    if (this.client.getState().initialized) return true;
    if (this.initializing) return this.whenReady();
    this.initializing = true;
    try {
      const settings = await this.getSettings();
      const ok = await this.client.initializeClient(settings, this.sessionId);
      this.settleWaiters(ok);
      return ok;
    } catch {
      this.settleWaiters(false);
      return false;
    } finally {
      this.initializing = false;
    }
  }

  /**
   * Resolves true once initialized (kicking the handshake off if the socket is
   * up and nobody has), false on timeout — a send that can't reach a session
   * turns into a visible error instead of today's silent no-op.
   */
  whenReady(): Promise<boolean> {
    if (this.client.getState().initialized) return Promise.resolve(true);
    if (!this.initializing && this.client.getState().socketConnected) void this.initialize();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.readyWaiters.delete(waiter);
        resolve(false);
      }, this.initTimeoutMs);
      const waiter = (ok: boolean) => {
        clearTimeout(timer);
        resolve(ok);
      };
      this.readyWaiters.add(waiter);
    });
  }

  private settleWaiters(ok: boolean) {
    const waiters = [...this.readyWaiters];
    this.readyWaiters.clear();
    for (const w of waiters) w(ok);
  }

  /** Detach from the client. `attach()` revives it; the snapshot listeners are
   *  left alone — they belong to whoever subscribed and unsubscribe themselves. */
  destroy(): void {
    this.detachClient?.();
    this.detachClient = null;
    this.settleWaiters(false);
  }
}
