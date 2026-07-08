import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { TransportClient } from './client';
import { createNannos } from './index';
import { CLIENT_ACTION_EXT } from './extensions';

class FakeSocket {
  connected = false;
  handlers = new Map<string, Array<(data?: unknown) => void>>();
  emitted: Array<{ event: string; payload: unknown }> = [];

  on(event: string, cb: (data?: unknown) => void) {
    const list = this.handlers.get(event) ?? [];
    list.push(cb);
    this.handlers.set(event, list);
    return this;
  }
  emit(event: string, payload?: unknown) {
    this.emitted.push({ event, payload });
    return this;
  }
  connectCount = 0;
  connect() {
    this.connected = true;
    this.connectCount += 1;
    return this;
  }
  disconnect() {
    this.connected = false;
  }
  /** Test helper: fire a server-sent event. */
  fire(event: string, data?: unknown) {
    for (const cb of this.handlers.get(event) ?? []) cb(data);
  }
}

function makeClient(cfg: Parameters<typeof TransportClient.prototype.constructor>[0] = {}) {
  const fake = new FakeSocket();
  const ioOpts: Record<string, unknown>[] = [];
  const client = new TransportClient(cfg, (_uri, opts) => {
    ioOpts.push(opts);
    return fake as unknown as Socket;
  });
  return { client, fake, ioOpts };
}

describe('TransportClient handshake', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('resolves true and stores agentInfo on successful client_initialized', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    fake.connected = true;
    fake.fire('connect');
    expect(client.getState().socketConnected).toBe(true);

    const init = client.initializeClient({ agentUrl: 'http://orch', model: 'm' }, 'sess-1');
    expect(fake.emitted[0]).toEqual({
      event: 'initialize_client',
      payload: { url: 'http://orch', customHeaders: {}, sessionId: 'sess-1' },
    });

    fake.fire('client_initialized', { status: 'success', agent: { name: 'Nannos' } });
    await expect(init).resolves.toBe(true);
    expect(client.getState().initialized).toBe(true);
    expect(client.getState().agentInfo).toEqual({ name: 'Nannos' });
  });

  it('resolves false on error status', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    const init = client.initializeClient({ agentUrl: 'u', model: 'm' }, 's');
    fake.fire('client_initialized', { status: 'error', error: 'nope' });
    await expect(init).resolves.toBe(false);
    expect(client.getState().initialized).toBe(false);
    expect(client.getState().agentInfo).toBeNull();
  });

  it('resolves false on timeout (15s default)', async () => {
    const { client } = makeClient();
    await client.connect();
    const init = client.initializeClient({ agentUrl: 'u', model: 'm' }, 's');
    vi.advanceTimersByTime(15_000);
    await expect(init).resolves.toBe(false);
  });

  it('resolves false when connect() was never called', async () => {
    const { client } = makeClient();
    await expect(client.initializeClient({ agentUrl: 'u', model: 'm' }, 's')).resolves.toBe(false);
  });
});

describe('TransportClient messaging and state', () => {
  it('guards sendMessage/cancelTask until the socket is connected', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    const payload = { id: '1', conversationId: 'c', message: 'hi', sessionId: 's' };
    expect(client.sendMessage(payload)).toBe(false);
    fake.connected = true;
    expect(client.sendMessage(payload)).toBe(true);
    expect(client.cancelTask('c')).toBe(true);
    expect(fake.emitted.map((e) => e.event)).toEqual(['send_message', 'cancel_task']);
  });

  it('fans agent_response out to subscribers and supports unsubscribe', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    const seen: unknown[] = [];
    const off = client.onAgentResponse((d) => seen.push(d));
    fake.fire('agent_response', { messageId: 'm1' });
    off();
    fake.fire('agent_response', { messageId: 'm2' });
    expect(seen).toEqual([{ messageId: 'm1' }]);
  });

  it('passes the on-behalf-of token in socket.io auth and resets state on disconnect event', async () => {
    const { client, fake, ioOpts } = makeClient({ getToken: () => 'tok-123' });
    await client.connect();
    // auth is a callback (invoked on each connect/reconnect for a fresh token).
    const authFn = ioOpts[0].auth as (cb: (d: Record<string, unknown>) => void) => void;
    expect(typeof authFn).toBe('function');
    let authData: Record<string, unknown> | undefined;
    authFn((d) => {
      authData = d;
    });
    await Promise.resolve();
    expect(authData).toEqual({ token: 'tok-123' });

    fake.fire('connect');
    fake.fire('client_initialized', { status: 'success', agent: {} });
    expect(client.getState().initialized).toBe(true);
    fake.fire('disconnect');
    expect(client.getState()).toEqual({ socketConnected: false, initialized: false, agentInfo: null });
  });

  it('is StrictMode-safe: disconnect() nulls the socket so a remount reconnects cleanly', async () => {
    // React 18 <StrictMode> double-invokes mount→unmount→mount. The provider calls
    // connect() → disconnect() → connect(). disconnect() must null the socket so the
    // second connect() actually re-creates it (not dead-end on `if (this.socket) return`).
    const fake = new FakeSocket();
    const factoryCalls: unknown[] = [];
    const client = new TransportClient({}, (_uri, opts) => {
      factoryCalls.push(opts);
      return fake as unknown as Socket;
    });
    await client.connect();
    expect(factoryCalls.length).toBe(1);
    await client.connect(); // guarded no-op while socket exists
    expect(factoryCalls.length).toBe(1);
    client.disconnect(); // StrictMode cleanup
    await client.connect(); // remount
    expect(factoryCalls.length).toBe(2); // socket re-created, not dead-ended
  });

  it('re-auths (reconnects) shortly before the access token expires', async () => {
    vi.useFakeTimers();
    // JWT expiring in 120s; re-auth is scheduled 60s before → fires at ~60s.
    const expSec = Math.floor(Date.now() / 1000) + 120;
    const payload = btoa(JSON.stringify({ exp: expSec })).replace(/=+$/, '');
    const jwt = `h.${payload}.s`;
    const { client, fake, ioOpts } = makeClient({ getToken: () => jwt });
    await client.connect();

    // socket.io would invoke the auth callback on connect; do it manually here.
    (ioOpts[0].auth as (cb: (d: Record<string, unknown>) => void) => void)(() => {});
    await Promise.resolve();
    await Promise.resolve();

    expect(fake.connectCount).toBe(0);
    vi.advanceTimersByTime(59_000);
    expect(fake.connectCount).toBe(0); // not yet
    vi.advanceTimersByTime(2_000); // past the 60s lead
    expect(fake.connectCount).toBe(1); // reconnected to refresh the token
    vi.useRealTimers();
  });
});

describe('TransportClient snapshots and extra events', () => {
  it('replays pendingHitl from a conversation_snapshot through the agent_response path', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    const responses: unknown[] = [];
    const snapshots: unknown[] = [];
    client.onAgentResponse((d) => responses.push(d));
    client.onConversationSnapshot((d) => snapshots.push(d));

    const pendingHitl = { kind: 'status-update', status: { state: 'input-required' } };
    fake.fire('conversation_snapshot', { conversationId: 'c1', inFlight: true, offset: 0, pendingHitl });

    // The prompt arrived while disconnected — it must render via the normal HITL
    // path, or the turn hangs waiting for an approval the user can never give.
    expect(responses).toEqual([pendingHitl]);
    expect(snapshots).toHaveLength(1);
  });

  it('does not stack socket handlers across onEvent unsubscribe/resubscribe cycles', async () => {
    const { client, fake } = makeClient();
    await client.connect();
    const seen: unknown[] = [];
    const off = client.onEvent('catalog_sync_progress', (d) => seen.push(d));
    off();
    client.onEvent('catalog_sync_progress', (d) => seen.push(d));

    fake.fire('catalog_sync_progress', { step: 1 });
    // One delivery and one underlying socket handler — a 0→1 re-attach per cycle
    // would fan the same callback out N times after N remounts.
    expect(seen).toEqual([{ step: 1 }]);
    expect(fake.handlers.get('catalog_sync_progress')).toHaveLength(1);
  });
});

describe('NannosCore.bindClientActions', () => {
  it('unwraps the status-update envelope and routes navigate to the host hook', async () => {
    const fake = new FakeSocket();
    const core = createNannos({}, () => fake as unknown as Socket);
    await core.connect();
    const navigate = vi.fn();
    const off = core.bindClientActions({ navigate });
    expect(core.clientActionsBound).toBe(true);

    fake.fire('agent_response', {
      kind: 'status-update',
      status: {
        message: {
          extensions: [CLIENT_ACTION_EXT],
          parts: [{ kind: 'data', data: { directive: { kind: 'navigate', to: '/campaigns' } } }],
        },
      },
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(navigate).toHaveBeenCalledWith('/campaigns');

    off();
    expect(core.clientActionsBound).toBe(false);
  });

  it('ignores plain streaming events without schema errors', async () => {
    const fake = new FakeSocket();
    const core = createNannos({}, () => fake as unknown as Socket);
    await core.connect();
    const navigate = vi.fn();
    core.bindClientActions({ navigate });
    fake.fire('agent_response', { kind: 'artifact-update', artifact: { parts: [{ kind: 'text', text: 'chunk' }] } });
    await new Promise((r) => setTimeout(r, 0));
    expect(navigate).not.toHaveBeenCalled();
  });
});
