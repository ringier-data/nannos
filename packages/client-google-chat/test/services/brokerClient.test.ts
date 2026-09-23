import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import {
  BrokerClient,
  BrokerError,
  BrokerSignInRequiredError,
  MintedToken,
} from '../../src/services/brokerClient.js';

type FetchCall = { url: string; init: { method: string; headers: Record<string, string>; body: string } };

function response(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

describe('BrokerClient', () => {
  let fetchMock: jest.Mock;
  let credentials: jest.Mock<(audience: string) => Promise<MintedToken>>;
  let broker: BrokerClient;

  const calls = (): FetchCall[] =>
    fetchMock.mock.calls.map(([url, init]) => ({ url: String(url), init: init as FetchCall['init'] }));

  beforeEach(() => {
    fetchMock = jest.fn() as unknown as jest.Mock;
    (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
    credentials = jest.fn(async () => ({ accessToken: 'svc-1', expiresAt: Date.now() + 300_000 }));
    broker = new BrokerClient({
      baseUrl: 'http://console:8080/',
      clientId: 'google-chat-client',
      serviceAudience: 'agent-console',
      getServiceCredentials: credentials,
    });
  });

  test('builds the authorize URL with client id, redirect URI and state', () => {
    const url = new URL(broker.authorizeUrl('https://gchat.example/api/v1/oauth/callback', 'gchat-auth-1-U1'));
    expect(url.origin + url.pathname).toBe('http://console:8080/api/v1/auth/broker/authorize');
    expect(url.searchParams.get('client_id')).toBe('google-chat-client');
    expect(url.searchParams.get('redirect_uri')).toBe('https://gchat.example/api/v1/oauth/callback');
    expect(url.searchParams.get('state')).toBe('gchat-auth-1-U1');
  });

  test('the authorize URL is built from the public URL when there is one', async () => {
    const publicBroker = new BrokerClient({
      baseUrl: 'http://console:8080',
      publicBaseUrl: 'https://console.example/',
      clientId: 'google-chat-client',
      serviceAudience: 'agent-console',
      getServiceCredentials: credentials,
    });
    const url = new URL(publicBroker.authorizeUrl('https://gchat.example/api/v1/oauth/callback', 's1'));
    expect(url.origin + url.pathname).toBe('https://console.example/api/v1/auth/broker/authorize');

    // The client's own calls stay on the in-cluster URL.
    fetchMock.mockImplementation(async () => response(200, { user_id: 'u1', sub: 'sub-1', groups: [] }));
    await publicBroker.redeem('code-1');
    expect(calls()[0].url).toBe('http://console:8080/api/v1/auth/broker/redeem');
  });

  test('redeems a code as the client itself', async () => {
    fetchMock.mockImplementation(async () => response(200, { user_id: 'u1', sub: 'sub-1', groups: [] }));

    const identity = await broker.redeem('code-1');

    expect(identity.sub).toBe('sub-1');
    const [call] = calls();
    expect(call.url).toBe('http://console:8080/api/v1/auth/broker/redeem');
    expect(call.init.headers.Authorization).toBe('Bearer svc-1');
    expect(JSON.parse(call.init.body)).toEqual({ code: 'code-1' });
    expect(credentials).toHaveBeenCalledWith('agent-console');
  });

  test('a refused code is a BrokerError', async () => {
    fetchMock.mockImplementation(async () => response(400, { detail: 'invalid_grant' }));
    await expect(broker.redeem('used')).rejects.toBeInstanceOf(BrokerError);
  });

  test('mints a token and turns expires_in into an expiry time', async () => {
    fetchMock.mockImplementation(async () => response(200, { access_token: 'at-1', expires_in: 600 }));
    const before = Date.now();

    const minted = await broker.mint('sub-1', 'orchestrator');

    expect(minted.accessToken).toBe('at-1');
    expect(minted.expiresAt).toBeGreaterThanOrEqual(before + 600_000);
    expect(JSON.parse(calls()[0].init.body)).toEqual({ sub: 'sub-1', audience: 'orchestrator' });
  });

  test('409 means the user must sign in again', async () => {
    fetchMock.mockImplementation(async () => response(409, { detail: 'The user has not signed in through this client' }));
    await expect(broker.mint('sub-1', 'orchestrator')).rejects.toBeInstanceOf(BrokerSignInRequiredError);
  });

  test('other mint failures are a BrokerError with the status', async () => {
    fetchMock.mockImplementation(async () => response(502, { detail: 'Keycloak is unreachable' }));
    await expect(broker.mint('sub-1', 'orchestrator')).rejects.toMatchObject({ status: 502 });
  });

  test('the service token is fetched once and reused', async () => {
    fetchMock.mockImplementation(async () => response(200, { access_token: 'at', expires_in: 600 }));
    await Promise.all([broker.mint('a', 'orchestrator'), broker.mint('b', 'orchestrator')]);
    await broker.mint('c', 'orchestrator');
    expect(credentials).toHaveBeenCalledTimes(1);
  });

  test('a 401 renews the service token once and retries', async () => {
    credentials
      .mockResolvedValueOnce({ accessToken: 'stale', expiresAt: Date.now() + 300_000 })
      .mockResolvedValueOnce({ accessToken: 'fresh', expiresAt: Date.now() + 300_000 });
    fetchMock
      .mockImplementationOnce(async () => response(401, {}))
      .mockImplementationOnce(async () => response(200, { access_token: 'at', expires_in: 600 }));

    await broker.mint('sub-1', 'orchestrator');

    expect(calls().map((c) => c.init.headers.Authorization)).toEqual(['Bearer stale', 'Bearer fresh']);
  });
});
