// @vitest-environment happy-dom
/**
 * ADR-0006: the agent's display name comes from the HOST's own well-known index,
 * same-origin and unauthenticated — never from a sub-agent id the page declares.
 */
import { describe, expect, it, vi } from 'vitest';
import { NannosCore } from './index';

function core(config: Record<string, unknown> = {}) {
  // No socket is opened until connect(); the io factory is never called here.
  return new NannosCore({ backendUrl: 'https://console.example', ...config } as never, (() => {
    throw new Error('not expected');
  }) as never);
}

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as unknown as Response;
}

describe('NannosCore.resolveHostAgentName', () => {
  it('reads x-nannos-agent.name from the page origin, without credentials, once', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ 'x-nannos-agent': { name: ' Alloy AI Assistant ' } }));
    const c = core();
    expect(await c.resolveHostAgentName(fetchImpl)).toBe('Alloy AI Assistant');
    expect(await c.resolveHostAgentName(fetchImpl)).toBe('Alloy AI Assistant');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${window.location.origin}/.well-known/agent-skills/index.json`);
    expect(init.credentials).toBe('omit');
  });

  it('is null when the host publishes nothing usable', async () => {
    expect(await core().resolveHostAgentName(vi.fn(async () => jsonResponse({}, false)))).toBeNull();
    expect(await core().resolveHostAgentName(vi.fn(async () => jsonResponse({ skills: [] })))).toBeNull();
    expect(await core().resolveHostAgentName(vi.fn(async () => jsonResponse({ 'x-nannos-agent': { name: 7 } })))).toBeNull();
    expect(
      await core().resolveHostAgentName(
        vi.fn(async () => {
          throw new TypeError('network');
        }),
      ),
    ).toBeNull();
  });
});

describe('NannosCore.isEmbedded', () => {
  it('is the bearer-token tell: getToken or auth means an embedded host, cookies mean the console', () => {
    expect(core().isEmbedded()).toBe(false);
    expect(core({ getToken: () => 'jwt' }).isEmbedded()).toBe(true);
    expect(
      core({ auth: { getAccessToken: async () => 'jwt', login: async () => undefined, logout: async () => undefined } }).isEmbedded(),
    ).toBe(true);
  });
});
