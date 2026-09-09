// @vitest-environment happy-dom
/**
 * The inspector's `auth` tab: the bearer the SDK actually presents.
 *
 * It is read through `config.getToken` — the same call the socket's auth
 * callback and every REST leg make — so what the tab shows cannot drift from
 * what crosses the wire. Two rules the tests pin down: the raw JWT stays
 * MASKED until the developer asks for it (this panel ends up in screenshots
 * and pasted bug reports, and the token is a live credential), and a bearer
 * that is missing or dead is announced on the COLLAPSED bar, where a
 * developer looking at a broken panel will actually see it.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { createNannos, type NannosConfig } from '../core';
import { NannosProvider, type NannosHostAdapter } from '../react';
import { AssistantPanel } from './assistant-panel';

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class FakeSocket {
  connected = false;
  on() {
    return this;
  }
  emit() {}
  connect() {}
  disconnect() {}
}

const ADAPTER: NannosHostAdapter = {
  defaults: { agentUrl: 'http://agent', model: 'm1' },
  api: { getUserSettings: async () => null },
};

/** Encode as a JWT segment does: UTF-8 → base64url, unpadded. */
function segment(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function makeJwt(payload: Record<string, unknown>): string {
  // Long enough that masking has something to hide.
  return `${segment({ alg: 'RS256', typ: 'JWT', kid: 'kid-1' })}.${segment(payload)}.${'s'.repeat(120)}`;
}

const LIVE_TOKEN = makeJwt({
  sub: 'user-1',
  preferred_username: 'erik',
  azp: 'nannos-embed',
  exp: Math.floor(Date.now() / 1000) + 300,
  iat: Math.floor(Date.now() / 1000),
});

function mountPanel(config: NannosConfig) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify({ conversations: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    ),
  );
  const core = createNannos(config, () => new FakeSocket() as unknown as Socket);
  render(
    <NannosProvider core={core}>
      <AssistantPanel shadow={false} adapter={ADAPTER} devMode header={false} />
    </NannosProvider>,
  );
}

const bar = () => document.querySelector('[data-slot="nannos-dev-inspector"]')!;
const openAuthTab = () => fireEvent.click(screen.getByRole('tab', { name: /^auth/ }));

describe('dev inspector — auth tab', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    localStorage.clear();
  });
  afterEach(cleanup);

  it('shows the token the SDK presents — masked, with its claims and its remaining life', async () => {
    mountPanel({ backendUrl: 'https://backend.example', getToken: () => LIVE_TOKEN });
    openAuthTab();

    // Read asynchronously (getToken may be a backend round trip), so wait.
    await waitFor(() => expect(bar().textContent).toMatch(/expires in \d/));
    const text = () => bar().textContent ?? '';

    // Masked: the head and tail are enough to recognize a token, the whole
    // string is a credential.
    expect(text()).toContain(LIVE_TOKEN.slice(0, 16));
    expect(text()).not.toContain(LIVE_TOKEN);
    // The claims are the part a developer needs, so they are shown outright.
    expect(text()).toContain('erik');
    expect(text()).toContain('preferred_username');
    expect(text()).toContain('nannos-embed');
    expect(text()).toContain('RS256');
    // Where it goes, and where it came from.
    expect(text()).toContain('https://backend.example');
    expect(text()).toContain('config.getToken()');
  });

  it('reveals the raw JWT only on request', async () => {
    mountPanel({ backendUrl: 'https://backend.example', getToken: () => LIVE_TOKEN });
    openAuthTab();
    const reveal = await screen.findByRole('button', { name: /reveal/ });

    fireEvent.click(reveal);

    expect(bar().textContent).toContain(LIVE_TOKEN);
    fireEvent.click(screen.getByRole('button', { name: /hide/ }));
    expect(bar().textContent).not.toContain(LIVE_TOKEN);
  });

  it('says so — on the collapsed bar — when the source has no token to give', async () => {
    // The socket connects into an `unauthenticated` state on an empty token;
    // without this line the panel just looks broken.
    mountPanel({ backendUrl: 'https://backend.example', getToken: () => '' });

    await screen.findByText('no token');
    // And it is on the bar itself, not hidden inside the tab.
    expect(bar().querySelector('summary')!.textContent).toContain('no token');
  });

  it('marks a dead token as expired rather than counting down past zero', async () => {
    const dead = makeJwt({ sub: 'user-1', exp: Math.floor(Date.now() / 1000) - 120 });
    mountPanel({ backendUrl: 'https://backend.example', getToken: () => dead });

    await screen.findByText('token expired');
    openAuthTab();
    expect(bar().textContent).toMatch(/expired.*ago|2m 0s ago/);
  });

  it('reports the console case as what it is: a cookie session, no bearer', async () => {
    // No `getToken` — same-origin, the session cookie carries the request.
    mountPanel({});
    openAuthTab();

    expect(await screen.findByText(/no bearer on this surface/)).toBeTruthy();
    // Nothing is claimed about a token that does not exist…
    expect(bar().textContent).not.toMatch(/expires in/);
    // …and the bar stays quiet: this is normal, not a fault.
    expect(bar().querySelector('summary')!.textContent).not.toContain('no token');
  });

  it('re-reads the token on demand rather than showing the one it cached', async () => {
    // A refresh happens outside this panel — the socket re-auths, the host's
    // BFF mints a new one — so the tab has to be able to ask again.
    let live = makeJwt({ sub: 'user-1', exp: Math.floor(Date.now() / 1000) + 300 });
    mountPanel({ backendUrl: 'https://backend.example', getToken: () => live });
    openAuthTab();
    await waitFor(() => expect(bar().textContent).toContain('user-1'));

    live = makeJwt({ sub: 'user-2', exp: Math.floor(Date.now() / 1000) + 300 });
    fireEvent.click(screen.getByRole('button', { name: /re-read/ }));

    await waitFor(() => expect(bar().textContent).toContain('user-2'));
  });
});
