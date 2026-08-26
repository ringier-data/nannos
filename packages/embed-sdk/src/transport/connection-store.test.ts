import { describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { TransportClient } from '../core/client';
import { ConnectionStore } from './connection-store';

/** Socket fake mirroring client.test.ts: handlers fired by name, emits captured. */
class FakeSocket {
  connected = false;
  emitted: Array<[string, unknown]> = [];
  private handlers = new Map<string, (data: unknown) => void>();
  on(event: string, cb: (data: unknown) => void) {
    this.handlers.set(event, cb);
    return this;
  }
  emit(event: string, payload: unknown) {
    this.emitted.push([event, payload]);
  }
  connect() {
    this.connected = true;
    this.fire('connect', undefined);
  }
  disconnect() {
    this.connected = false;
    this.fire('disconnect', undefined);
  }
  fire(event: string, data: unknown) {
    this.handlers.get(event)?.(data);
  }
}

function setup() {
  const fake = new FakeSocket();
  const client = new TransportClient({}, () => fake as unknown as Socket);
  const getSettings = vi.fn(async () => ({ agentUrl: 'http://agent', model: 'm1' }));
  const store = new ConnectionStore(client, getSettings, 'sess-1', 300);
  return { fake, client, store, getSettings };
}

describe('ConnectionStore', () => {
  it('initialize runs the handshake with resolved settings and settles whenReady(true)', async () => {
    const { fake, client, store, getSettings } = setup();
    await client.connect();
    fake.connect();

    const ready = store.whenReady();
    // whenReady kicked off initialize → the emit is on the wire; ack it.
    await vi.waitFor(() => expect(fake.emitted.some(([e]) => e === 'initialize_client')).toBe(true));
    const [, payload] = fake.emitted.find(([e]) => e === 'initialize_client')!;
    expect(payload).toMatchObject({ url: 'http://agent', sessionId: 'sess-1' });
    fake.fire('client_initialized', { status: 'success', agent: { name: 'Orchestrator' } });

    await expect(ready).resolves.toBe(true);
    expect(getSettings).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot()).toMatchObject({ initialized: true, agentName: 'Orchestrator' });
  });

  it('whenReady times out to FALSE when nothing answers (a send becomes a visible error)', async () => {
    const { store, client, fake } = setup();
    await client.connect();
    fake.connect();
    // Never ack initialize_client.
    await expect(store.whenReady()).resolves.toBe(false);
  });

  it('re-initializes automatically after a reconnect (new server session)', async () => {
    const { fake, client, store } = setup();
    await client.connect();
    fake.connect();
    void store.initialize();
    await vi.waitFor(() => expect(fake.emitted.filter(([e]) => e === 'initialize_client')).toHaveLength(1));
    fake.fire('client_initialized', { status: 'success' });
    expect(store.getSnapshot().initialized).toBe(true);
    await Promise.resolve(); // let initialize() release its in-flight latch

    // Drop + reconnect: the fresh session must be re-initialized without help.
    fake.disconnect();
    expect(store.getSnapshot().initialized).toBe(false);
    fake.connect();
    await vi.waitFor(() =>
      expect(fake.emitted.filter(([e]) => e === 'initialize_client')).toHaveLength(2),
    );
    fake.fire('client_initialized', { status: 'success' });
    expect(store.getSnapshot().initialized).toBe(true);
  });
});
