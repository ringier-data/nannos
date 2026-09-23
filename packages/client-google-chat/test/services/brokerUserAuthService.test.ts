import { describe, test, expect, beforeEach, jest } from '@jest/globals';
import { BrokerUserAuthService } from '../../src/services/brokerUserAuthService.js';
import { BrokerClient, BrokerError, BrokerSignInRequiredError } from '../../src/services/brokerClient.js';
import { Config } from '../../src/config/config.js';
import type { IOAuthStateStore, IUserAuthStorage, UserAuthToken } from '../../src/storage/types.js';

/** An in-memory user_auth table keyed like the real one. */
class MemoryUserAuthStorage {
  rows = new Map<string, UserAuthToken>();
  key = (userId: string, projectId: string) => `${userId}:${projectId}`;
  saveToken = jest.fn(async (token: UserAuthToken) => {
    this.rows.set(this.key(token.userId, token.projectId), token);
  });
  getToken = jest.fn(async (userId: string, projectId: string) => this.rows.get(this.key(userId, projectId)) ?? null);
  deleteToken = jest.fn(async (userId: string, projectId: string) => {
    this.rows.delete(this.key(userId, projectId));
  });
}

const config = {
  baseUrl: 'https://gchat.example',
  oidc: { orchestratorAudience: 'orchestrator' },
} as unknown as Config;

describe('BrokerUserAuthService', () => {
  let storage: MemoryUserAuthStorage;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type AnyMock = jest.Mock<(...args: any[]) => any>;
  let broker: { authorizeUrl: AnyMock; redeem: AnyMock; mint: AnyMock };
  let service: BrokerUserAuthService;

  beforeEach(() => {
    storage = new MemoryUserAuthStorage();
    broker = {
      authorizeUrl: jest.fn((redirectUri: string, state: string) => `https://console/authorize?r=${redirectUri}&s=${state}`),
      redeem: jest.fn(async () => ({ user_id: 'u1', sub: 'sub-1', groups: [] })),
      mint: jest.fn(async (_sub: string, audience: string) => ({
        accessToken: `token-for-${audience}`,
        expiresAt: Date.now() + 3600_000,
      })),
    };
    service = new BrokerUserAuthService(
      storage as unknown as IUserAuthStorage,
      broker as unknown as BrokerClient,
      config,
      { set: jest.fn() } as unknown as IOAuthStateStore
    );
  });

  async function signIn() {
    return service.completeOAuthFlow(
      'U1',
      'P1',
      'https://gchat.example/api/v1/oauth/callback?code=code-1&state=s1',
      'unused-verifier',
      's1'
    );
  }

  test('the sign-in link goes to the broker and comes back to the usual callback', async () => {
    const url = await service.getAuthorizationUrl('s1', 'P1', 'verifier');
    expect(broker.authorizeUrl).toHaveBeenCalledWith('https://gchat.example/api/v1/oauth/callback', 's1');
    expect(url).toContain('https://console/authorize');
  });

  test('completing the sign-in stores only who the user is', async () => {
    const row = await signIn();

    expect(broker.redeem).toHaveBeenCalledWith('code-1');
    expect(row).toMatchObject({ userId: 'U1', projectId: 'P1', oidcSub: 'sub-1', authMode: 'broker' });
    expect(row.accessToken).toBeUndefined();
    expect(row.refreshToken).toBeUndefined();
    expect(await service.isUserAuthorized('U1', 'P1')).toBe(true);
  });

  test('a callback without a code fails', async () => {
    await expect(
      service.completeOAuthFlow('U1', 'P1', 'https://gchat.example/api/v1/oauth/callback?state=s1', 'v', 's1')
    ).rejects.toThrow('no code');
  });

  test('a local row is not a broker sign-in', async () => {
    storage.rows.set('U1:P1', { userId: 'U1', projectId: 'P1', oidcSub: 'sub-1', authMode: 'local', createdAt: 0, updatedAt: 0 });
    expect(await service.isUserAuthorized('U1', 'P1')).toBe(false);
    expect(await service.getOrchestratorToken('U1', 'P1')).toBeNull();
    expect(broker.mint).not.toHaveBeenCalled();
  });

  test('tokens are minted per audience and cached', async () => {
    await signIn();

    expect(await service.getOrchestratorToken('U1', 'P1')).toBe('token-for-orchestrator');
    expect(await service.getOrchestratorToken('U1', 'P1')).toBe('token-for-orchestrator');
    expect(await service.getTokenForAudience('U1', 'P1', 'agent-console')).toBe('token-for-agent-console');

    expect(broker.mint.mock.calls).toEqual([
      ['sub-1', 'orchestrator'],
      ['sub-1', 'agent-console'],
    ]);
  });

  test('a token close to its expiry is minted again', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => ({ accessToken: 'short', expiresAt: Date.now() + 1000 }));

    await service.getOrchestratorToken('U1', 'P1');
    await new Promise((resolve) => setTimeout(resolve, 600)); // past half the lifetime
    await service.getOrchestratorToken('U1', 'P1');

    expect(broker.mint).toHaveBeenCalledTimes(2);
  });

  test('when the broker says sign in again, the sign-in is forgotten', async () => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw new BrokerSignInRequiredError('HTTP 409');
    });

    expect(await service.getOrchestratorToken('U1', 'P1')).toBeNull();
    expect(storage.deleteToken).toHaveBeenCalledWith('U1', 'P1');
    expect(await service.isUserAuthorized('U1', 'P1')).toBe(false);
  });

  test.each([
    ['an audience this client may not have', new BrokerError('HTTP 403', 403)],
    ['Keycloak down', new BrokerError('HTTP 502', 502)],
    ['the broker unreachable', new TypeError('fetch failed')],
  ])('%s is thrown, not answered with "sign in again", and keeps the sign-in', async (_case, failure) => {
    await signIn();
    broker.mint.mockImplementationOnce(async () => {
      throw failure;
    });

    // null would make the caller ask for a sign-in, which cannot fix any of these.
    await expect(service.getOrchestratorToken('U1', 'P1')).rejects.toBe(failure);
    expect(storage.deleteToken).not.toHaveBeenCalled();
    expect(await service.isUserAuthorized('U1', 'P1')).toBe(true);
  });

  test('revoking forgets the sign-in and the cached tokens', async () => {
    await signIn();
    await service.getOrchestratorToken('U1', 'P1');

    await service.revokeUserAuthorization('U1', 'P1');

    expect(await service.getOrchestratorToken('U1', 'P1')).toBeNull();
    expect(broker.mint).toHaveBeenCalledTimes(1);
  });

  test('the sign-in state is stored before the link is handed out', async () => {
    let persist!: () => void;
    const set = jest.fn(() => new Promise<void>((resolve) => (persist = resolve)));
    const withSlowStore = new BrokerUserAuthService(
      storage as unknown as IUserAuthStorage,
      broker as unknown as BrokerClient,
      config,
      { set } as unknown as IOAuthStateStore
    );

    let stored = false;
    const storing = withSlowStore.storeAuthState('s1', 'U1', 'P1').then(() => (stored = true));
    await Promise.resolve();
    expect(stored).toBe(false);
    persist();
    await storing;
    expect(stored).toBe(true);

    // A failed write fails the call instead of becoming an unhandled rejection.
    set.mockImplementationOnce(() => Promise.reject(new Error('db down')));
    await expect(withSlowStore.storeAuthState('s2', 'U1', 'P1')).rejects.toThrow('db down');
  });
});
