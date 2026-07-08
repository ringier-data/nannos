import { describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { NannosCore } from './index';
import type { NannosAuth } from './types';

/** Minimal fake socket mirroring client.test.ts. */
class FakeSocket {
  connected = false;
  handlers = new Map<string, Array<(data?: unknown) => void>>();
  connectCount = 0;
  disconnectCount = 0;
  on(event: string, cb: (data?: unknown) => void) {
    const list = this.handlers.get(event) ?? [];
    list.push(cb);
    this.handlers.set(event, list);
    return this;
  }
  emit() {
    return this;
  }
  connect() {
    this.connected = true;
    this.connectCount += 1;
    return this;
  }
  disconnect() {
    this.connected = false;
    this.disconnectCount += 1;
  }
  fire(event: string, data?: unknown) {
    for (const cb of this.handlers.get(event) ?? []) cb(data);
  }
}

function makeCore(cfg: ConstructorParameters<typeof NannosCore>[0]) {
  const fake = new FakeSocket();
  const core = new NannosCore(cfg, () => fake as unknown as Socket);
  return { core, fake };
}

/** A controllable auth strategy. */
function fakeAuth(overrides: Partial<NannosAuth> & { authed?: boolean } = {}): NannosAuth & { authed: boolean } {
  const state = { authed: overrides.authed ?? false };
  return {
    authed: state.authed,
    getAccessToken: overrides.getAccessToken ?? (async () => (state.authed ? 'tok' : null)),
    login: overrides.login ?? (async () => { state.authed = true; return 'tok'; }),
    isAuthenticated: overrides.isAuthenticated ?? (() => state.authed),
    logout: overrides.logout ?? (() => { state.authed = false; }),
  } as NannosAuth & { authed: boolean };
}

describe('NannosCore auth resolution', () => {
  it('host-token path wins when both getToken and auth are set (auth ignored)', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { core } = makeCore({ getToken: () => 't', auth: fakeAuth() });
    expect(core.auth).toBeNull();
    expect(core.needsLogin()).toBe(false);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('needsLogin reflects an unauthenticated self-login strategy', () => {
    const { core } = makeCore({ backendUrl: 'https://b', auth: fakeAuth({ authed: false }) });
    expect(core.needsLogin()).toBe(true);
    expect(core.status).toBe('unauthenticated');
  });

  it('an already-authenticated strategy is not unauthenticated', () => {
    const { core } = makeCore({ backendUrl: 'https://b', auth: fakeAuth({ authed: true }) });
    expect(core.needsLogin()).toBe(false);
    expect(core.status).toBe('disconnected');
  });
});

describe('NannosCore status model', () => {
  it('starts disconnected and moves to connected on successful handshake', () => {
    const { core, fake } = makeCore({ backendUrl: 'https://b', getToken: () => 't' });
    const seen: string[] = [];
    core.onStatusChange((s) => seen.push(s));
    expect(seen[0]).toBe('disconnected');

    void core.connect();
    fake.fire('connect');
    fake.fire('client_initialized', { status: 'success', agent: {} });
    expect(core.status).toBe('connected');
    expect(seen).toContain('connecting');
    expect(seen).toContain('connected');
  });

  it('never calls auth.login() on connect-on-mount (silent token only)', async () => {
    const auth = fakeAuth({ authed: false });
    const loginSpy = vi.spyOn(auth, 'login');
    const getTokenSpy = vi.spyOn(auth, 'getAccessToken');
    const { core } = makeCore({ backendUrl: 'https://b', auth });
    await core.connect();
    expect(loginSpy).not.toHaveBeenCalled();
    // The transport's auth callback pulls the (null→'') silent token, not login.
    expect(core.needsLogin()).toBe(true);
    void getTokenSpy; // getAccessToken is wired as the silent token source
  });

  it('login() authenticates, reconnects, and flips status toward connected', async () => {
    const auth = fakeAuth({ authed: false });
    const { core, fake } = makeCore({ backendUrl: 'https://b', auth });
    await core.connect();
    fake.fire('connect'); // socket up but unauthenticated
    expect(core.needsLogin()).toBe(true);

    await core.login();
    expect(core.needsLogin()).toBe(false);
    expect(fake.disconnectCount).toBeGreaterThan(0); // reauth() cycled the socket

    fake.fire('client_initialized', { status: 'success', agent: {} });
    expect(core.status).toBe('connected');
  });

  it('login() presents the token even when the initial tokenless socket never connected', async () => {
    // Repro of the "always Disconnected until a hard refresh" bug: the provider's
    // silent connect-on-mount CREATES a socket but the server rejects it (no token),
    // so `socketConnected` stays false while `this.socket` is set. login() must
    // still cycle that socket (reauth) to present the token — not no-op on connect().
    const auth = fakeAuth({ authed: false });
    const { core, fake } = makeCore({ backendUrl: 'https://b', auth });
    await core.connect(); // socket created; NO 'connect' fired → socketConnected false
    expect(core.needsLogin()).toBe(true);
    const disconnectsBefore = fake.disconnectCount;

    await core.login();
    // reauth() must have cycled the existing socket so the auth callback re-runs.
    expect(fake.disconnectCount).toBeGreaterThan(disconnectsBefore);

    fake.fire('connect');
    fake.fire('client_initialized', { status: 'success', agent: {} });
    expect(core.status).toBe('connected');
  });

  it('forwards transport connect_error to onError', async () => {
    const { core, fake } = makeCore({ backendUrl: 'https://b', getToken: () => 't' });
    const errors: Array<{ type: string; message: string }> = [];
    core.onError((e) => errors.push(e));
    await core.connect();
    fake.fire('connect_error', new Error('boom'));
    expect(errors).toContainEqual(expect.objectContaining({ type: 'connection', message: 'boom' }));
  });

  it('emits an init error when initialize_client is rejected', async () => {
    const { core, fake } = makeCore({ backendUrl: 'https://b', getToken: () => 't' });
    const errors: Array<{ type: string }> = [];
    core.onError((e) => errors.push(e));
    await core.connect();
    fake.fire('client_initialized', { status: 'error', error: 'nope' });
    expect(errors.some((e) => e.type === 'init')).toBe(true);
  });

  it('emits an auth error when login() fails (in addition to rethrowing)', async () => {
    const auth = fakeAuth({ authed: false, login: async () => { throw new Error('popup blocked'); } });
    const { core } = makeCore({ backendUrl: 'https://b', auth });
    const errors: Array<{ type: string }> = [];
    core.onError((e) => errors.push(e));
    await expect(core.login()).rejects.toThrow('popup blocked');
    expect(errors.some((e) => e.type === 'auth')).toBe(true);
  });

  it('login() failure flips status to authError and rethrows', async () => {
    const auth = fakeAuth({ authed: false, login: async () => { throw new Error('popup blocked'); } });
    const { core } = makeCore({ backendUrl: 'https://b', auth });
    await expect(core.login()).rejects.toThrow('popup blocked');
    expect(core.status).toBe('authError');
  });

  it('logout() drops the token and disconnects', async () => {
    const auth = fakeAuth({ authed: true });
    const { core, fake } = makeCore({ backendUrl: 'https://b', auth });
    await core.connect();
    fake.fire('connect');
    fake.fire('client_initialized', { status: 'success', agent: {} });
    expect(core.status).toBe('connected');

    core.logout();
    expect(core.needsLogin()).toBe(true);
    expect(core.status).toBe('unauthenticated');
  });
});
